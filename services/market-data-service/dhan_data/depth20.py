"""dhan_data/depth20.py - Dhan 20-level market depth over WebSocket (group 286). OPT-IN: DHAN_DEPTH20_ENABLED=1.

Why: the REST quote carries 5 levels (`quotes.depth_fields`). A scalper order of a few lakh rupees in a small cap
can eat more than 5 levels, so the 5-level book under-states how thin the name is. This keeps the 20-level book of
the few symbols somebody is asking about right now and answers "how far would qty N move the price?".

How it works (on demand, nothing is subscribed until somebody asks):
  GET /depth/{symbol}[?qty=&slip_pct=]  ->  depth20.get(...)  registers the symbol, waits briefly for the first
  book, and returns the 20-level summary. A symbol that nobody asks about for DHAN_DEPTH20_TTL_S seconds is
  unsubscribed again. Dhan allows 50 instruments per connection; the least recently asked symbol is dropped first.

Spec (https://dhanhq.co/docs/v2/full-market-depth/, read 2026-10-09; NOT yet checked against a live capture):
  endpoint     wss://depth-api-feed.dhan.co/twentydepth?token=..&clientId=..&authType=2   (token in URL: never logged)
  subscribe    {"RequestCode": 23, "InstrumentCount": n, "InstrumentList": [{"ExchangeSegment","SecurityId"}]}  (<= 50)
  unsubscribe  RequestCode 24 (annexure "Unsubscribe - Full Market Depth")
  header       12 bytes: int16 message length | u8 response code | u8 exchange segment | int32 security id | u32 sequence
               NOTE the order differs from the 5-level live feed header (code first there, length first here)
  depth packet header + 20 x (float64 price, uint32 quantity, uint32 orders) = 12 + 320 = 332 bytes
               code 41 = bid (buy) side, code 51 = ask (sell) side; each message carries one side of one instrument
               and several are stacked one after another in a single websocket message
  disconnect   code 50, int16 reason at bytes 13-14 (805 = more than 5 connections, 807-810 = token problems)
  Only NSE equity and derivatives are enabled for full depth: indices (IDX_I) are refused here.
Byte order is assumed little-endian like the live feed; if /internal/dhan-status shows `bad_packets` or no books while
packets arrive, capture one frame and check it. Unknown codes are counted, never guessed at.

Everything fails OPEN for callers: disabled, no credentials, warming, stale, unsupported symbol all answer
{"available": false, "reason": ...} with HTTP 200 and the caller carries on without a depth-20 answer.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import struct
import threading
import time
from collections import OrderedDict
from typing import Dict, List, Optional, Set, Tuple

from . import client, config, creds, quotes, scrip_master
from .errors import DhanAuthError, DhanNotConfigured, DhanRateLimitError

logger = logging.getLogger("dhan-data.depth20")

LEVELS = 20
CODE_BID, CODE_ASK, CODE_DISCONNECT = 41, 51, 50
REQ_SUBSCRIBE_DEPTH, REQ_UNSUBSCRIBE_DEPTH = 23, 24
HEADER = struct.Struct("<HBBII")         # length, response code, segment, security id, sequence  (12 bytes)
_LEVEL = struct.Struct("<dII")           # price, quantity, orders                                  (16 bytes)
PACKET_SIZE = HEADER.size + LEVELS * _LEVEL.size       # 332
MAX_INSTRUMENTS = 50                     # Dhan: at most 50 instruments on one 20-level connection
SEG_CODES = {0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO", 3: "NSE_CURRENCY", 4: "BSE_EQ", 5: "MCX_COMM",
             7: "BSE_CURRENCY", 8: "BSE_FNO"}
_AUTH_REASONS = {806, 807, 808, 809, 810}

_lock = threading.Lock()
_wanted: "OrderedDict[str, dict]" = OrderedDict()     # symbol key -> {"ident": (seg, sid), "asked": monotonic}
_books: Dict[str, dict] = {}                          # symbol key -> {"bids","asks","bid_at","ask_at"}
_rev: Dict[Tuple[str, int], str] = {}                 # (segment, security id) -> symbol key (subscribed or wanted)
_stats = {"connected": False, "connects": 0, "packets": 0, "bid_packets": 0, "ask_packets": 0, "bad_packets": 0,
          "unknown_codes": {}, "unmatched": 0, "subscribed": 0, "last_packet_at": None, "last_error": None,
          "last_disconnect_code": None}
_thread: Optional[threading.Thread] = None
_stop = threading.Event()


# ── packet parsing (pure, tested with struct.pack) ──────────────────────────────────────────────────────────────
def parse_side(data: bytes, offset: int = 0) -> Optional[dict]:
    """One 332-byte bid/ask packet at `offset` -> {"side","segment","security_id","levels":[(price, qty, orders)]}.
    Empty slots (price <= 0 or quantity 0) are dropped; levels stay in the order Dhan sent them."""
    if len(data) - offset < PACKET_SIZE:
        return None
    _length, code, seg_code, sid, _seq = HEADER.unpack_from(data, offset)
    if code not in (CODE_BID, CODE_ASK):
        return None
    levels: List[Tuple[float, int, int]] = []
    base = offset + HEADER.size
    for i in range(LEVELS):
        price, qty, orders = _LEVEL.unpack_from(data, base + i * _LEVEL.size)
        if math.isfinite(price) and 0 < price < 1e9 and qty > 0:
            levels.append((price, int(qty), int(orders)))
    return {"kind": "bid" if code == CODE_BID else "ask", "segment": SEG_CODES.get(seg_code),
            "security_id": sid, "levels": levels}


def parse_message(data: bytes) -> List[dict]:
    """A websocket message holds one or more stacked packets. Returns every packet parsed from it:
    {"kind": "bid"|"ask", ...}, {"kind": "disconnect", "reason": int|None}, {"kind": "unknown", "code": n}, or
    {"kind": "bad"} once when the rest of the message is too short / garbled to walk further."""
    out: List[dict] = []
    if not isinstance(data, (bytes, bytearray)):
        return [{"kind": "bad"}]
    pos, n = 0, len(data)
    while pos < n:
        if n - pos < HEADER.size:
            out.append({"kind": "bad"})
            break
        length, code, seg_code, sid, _seq = HEADER.unpack_from(data, pos)
        if code in (CODE_BID, CODE_ASK):
            pkt = parse_side(data, pos)
            if pkt is None:
                out.append({"kind": "bad"})
                break
            out.append(pkt)
            pos += PACKET_SIZE
        elif code == CODE_DISCONNECT:
            reason = None
            if n - pos >= HEADER.size + 2:
                reason = struct.unpack_from("<h", data, pos + HEADER.size)[0]
            elif n - pos >= 10:
                reason = struct.unpack_from("<h", data, pos + 8)[0]
            out.append({"kind": "disconnect", "reason": reason, "segment": SEG_CODES.get(seg_code),
                        "security_id": sid})
            break
        else:
            out.append({"kind": "unknown", "code": code, "segment": SEG_CODES.get(seg_code), "security_id": sid})
            if HEADER.size <= length <= n - pos:     # skip an unknown packet only when its own length is plausible
                pos += length
            else:
                break
    return out


def build_subscribe_messages(instruments: List[Tuple[str, int]], request_code: int = REQ_SUBSCRIBE_DEPTH) -> List[str]:
    """JSON messages, at most 50 instruments each (the connection limit)."""
    msgs = []
    for i in range(0, len(instruments), MAX_INSTRUMENTS):
        part = instruments[i:i + MAX_INSTRUMENTS]
        msgs.append(json.dumps({
            "RequestCode": request_code, "InstrumentCount": len(part),
            "InstrumentList": [{"ExchangeSegment": seg, "SecurityId": str(sid)} for seg, sid in part]}))
    return msgs


# ── book maths (pure) ───────────────────────────────────────────────────────────────────────────────────────────
def _walk(levels: List[Tuple[float, int, int]], qty: float) -> Tuple[float, float]:
    """Walk levels (best first) for `qty` shares. Returns (filled quantity, notional spent)."""
    left, spent, filled = float(qty), 0.0, 0.0
    for price, q, _o in levels:
        if left <= 0:
            break
        take = min(left, q)
        spent += take * price
        filled += take
        left -= take
    return filled, spent


def summarize(bids: List[tuple], asks: List[tuple], qty: Optional[float] = None,
              slip_pct: Optional[float] = None) -> Optional[dict]:
    """20-level summary of one book, or None when either side is empty or the book is crossed (ask below bid; a locked
    book, ask == bid, is kept with spread 0 like the 5-level depth_fields).
    Fields: best_bid, best_ask, spread_pct (of mid), bid_levels, ask_levels, bid_qty_20, ask_qty_20,
    bid_value_20, ask_value_20, book_value_20 (Rs, both sides), imbalance_20 ((bid-ask)/(bid+ask) by quantity),
    bid_depth_pct / ask_depth_pct (how far the 20th level sits from the touch).
    qty given:      buy_* = what BUYING qty shares from the asks costs (buy_avg_price, buy_impact_pct vs best ask,
                    buy_complete False when the 20 levels hold fewer than qty shares) and sell_* the same for SELLING
                    qty into the bids (sell_avg_price, sell_impact_pct vs best bid).
    slip_pct given: buy_qty_within_slip / sell_qty_within_slip = shares available up to slip_pct % beyond the touch."""
    bids = sorted(bids, key=lambda t: -t[0])
    asks = sorted(asks, key=lambda t: t[0])
    if not bids or not asks:
        return None
    bid, ask = bids[0][0], asks[0][0]
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    bq, aq = sum(q for _, q, _ in bids), sum(q for _, q, _ in asks)
    bv, av = sum(p * q for p, q, _ in bids), sum(p * q for p, q, _ in asks)
    out = {
        "best_bid": bid, "best_ask": ask, "spread_pct": round((ask - bid) / mid * 100.0, 4),
        "bid_levels": len(bids), "ask_levels": len(asks), "bid_qty_20": bq, "ask_qty_20": aq,
        "bid_value_20": round(bv, 2), "ask_value_20": round(av, 2), "book_value_20": round(bv + av, 2),
        "imbalance_20": round((bq - aq) / (bq + aq), 4) if (bq + aq) else None,
        "bid_depth_pct": round((bid - bids[-1][0]) / bid * 100.0, 4),
        "ask_depth_pct": round((asks[-1][0] - ask) / ask * 100.0, 4),
    }
    if qty is not None and qty > 0:
        bf, bs = _walk(asks, qty)
        sf, ss = _walk(bids, qty)
        out["qty"] = qty
        out["buy_complete"] = bf >= qty
        out["buy_filled_qty"] = bf
        out["buy_avg_price"] = round(bs / bf, 4) if bf else None
        out["buy_impact_pct"] = round((bs / bf - ask) / ask * 100.0, 4) if bf else None
        out["sell_complete"] = sf >= qty
        out["sell_filled_qty"] = sf
        out["sell_avg_price"] = round(ss / sf, 4) if sf else None
        out["sell_impact_pct"] = round((bid - ss / sf) / bid * 100.0, 4) if sf else None
    if slip_pct is not None and slip_pct >= 0:
        out["slip_pct"] = slip_pct
        out["buy_qty_within_slip"] = sum(q for p, q, _ in asks if p <= ask * (1 + slip_pct / 100.0) + 1e-9)
        out["sell_qty_within_slip"] = sum(q for p, q, _ in bids if p >= bid * (1 - slip_pct / 100.0) - 1e-9)
    return out


# ── state ────────────────────────────────────────────────────────────────────────────────────────────────────────
def apply_packet(pkt: dict, now_mono: Optional[float] = None) -> Optional[str]:
    """Store one parsed bid/ask packet. Returns the symbol key it updated, or None (unknown instrument / empty)."""
    kind = pkt.get("kind")
    if kind not in ("bid", "ask"):
        return None
    now = time.monotonic() if now_mono is None else now_mono
    with _lock:
        key = _rev.get((pkt.get("segment"), pkt.get("security_id")))
        if key is None or key not in _wanted:
            _stats["unmatched"] += 1
            return None
        b = _books.setdefault(key, {"bids": [], "asks": [], "bid_at": None, "ask_at": None})
        if kind == "bid":
            b["bids"], b["bid_at"] = list(pkt.get("levels") or []), now
        else:
            b["asks"], b["ask_at"] = list(pkt.get("levels") or []), now
    return key


def ident_for(symbol: str) -> Optional[Tuple[str, int]]:
    """(segment, security id) when this symbol can have a 20-level book (NSE equity only), else None."""
    ident = scrip_master.security_id(symbol)
    if ident and ident[0] == "NSE_EQ":
        return ident
    return None


def watch(symbol: str, now_mono: Optional[float] = None) -> str:
    """Register (or refresh) interest in a symbol. Returns "ok" or "unsupported" (no NSE_EQ id: indices, unknown
    names). When the instrument limit is reached the least recently asked symbol is dropped to make room."""
    key = quotes.key_for(symbol)
    now = time.monotonic() if now_mono is None else now_mono
    with _lock:
        cur = _wanted.get(key)
        if cur is not None:
            cur["asked"] = now
            _wanted.move_to_end(key)
            return "ok"
    ident = ident_for(key)
    if ident is None:
        return "unsupported"
    with _lock:
        _wanted[key] = {"ident": ident, "asked": now}
        _wanted.move_to_end(key)
        _rev[ident] = key
        limit = min(config.depth20_max_instruments(), MAX_INSTRUMENTS)
        while len(_wanted) > limit:
            old, meta = _wanted.popitem(last=False)
            _books.pop(old, None)
            _rev.pop(meta["ident"], None)
    return "ok"


def expire(now_mono: Optional[float] = None) -> List[str]:
    """Drop symbols nobody asked about for DHAN_DEPTH20_TTL_S. Returns the keys removed."""
    now = time.monotonic() if now_mono is None else now_mono
    ttl = config.depth20_ttl_s()
    gone = []
    with _lock:
        for k in [k for k, m in _wanted.items() if now - m["asked"] > ttl]:
            meta = _wanted.pop(k)
            _books.pop(k, None)
            _rev.pop(meta["ident"], None)
            gone.append(k)
    return gone


def get(symbol: str, qty: Optional[float] = None, slip_pct: Optional[float] = None,
        wait_s: Optional[float] = None) -> dict:
    """Public answer for GET /depth/{symbol}. Never raises. Always has `symbol` and `available`."""
    key = quotes.key_for(symbol)
    base = {"symbol": key, "available": False, "source": "dhan_depth20"}
    try:
        if not config.enabled():
            return {**base, "reason": "dhan_disabled"}
        if not config.depth20_enabled():
            return {**base, "reason": "depth20_disabled"}
        if not client.available():
            return {**base, "reason": "dhan_unavailable"}
        st = watch(key)
        if st != "ok":
            return {**base, "reason": st}
        deadline = time.monotonic() + (config.depth20_wait_s() if wait_s is None else max(0.0, wait_s))
        while True:
            snap = _snapshot(key, qty, slip_pct)
            if snap is not None or time.monotonic() >= deadline:
                break
            time.sleep(0.1)
        if snap is None:
            return {**base, "reason": "warming"}
        age = snap.pop("_age_s")
        out = {**base, **snap, "age_s": round(age, 2), "fresh": age <= config.depth20_stale_s()}
        out["available"] = out["fresh"]
        if not out["fresh"]:
            out["reason"] = "stale"
        return out
    except Exception as e:  # noqa: BLE001 - depth must never break the caller
        return {**base, "reason": f"error:{type(e).__name__}"}


def _snapshot(key: str, qty, slip_pct) -> Optional[dict]:
    with _lock:
        b = _books.get(key)
        if not b or b["bid_at"] is None or b["ask_at"] is None:
            return None
        bids, asks = list(b["bids"]), list(b["asks"])
        age = time.monotonic() - min(b["bid_at"], b["ask_at"])
    s = summarize(bids, asks, qty, slip_pct)
    if s is None:
        return None
    s["_age_s"] = age
    return s


# ── connection loop ─────────────────────────────────────────────────────────────────────────────────────────────
def _diff(subscribed: Set[Tuple[str, int]]) -> Tuple[List[Tuple[str, int]], List[Tuple[str, int]]]:
    with _lock:
        want = {m["ident"] for m in _wanted.values()}
    return sorted(want - subscribed), sorted(subscribed - want)


async def _session() -> None:
    import websockets
    client_id, token = creds.get_credentials()
    url = f"{config.depth20_url()}?token={token}&clientId={client_id}&authType=2"      # never logged
    subscribed: Set[Tuple[str, int]] = set()
    idle_since = time.monotonic()
    async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=2 ** 20) as ws:
        with _lock:
            _stats["connected"] = True
            _stats["connects"] += 1
        logger.info("dhan 20-level depth websocket connected")
        last_sync = 0.0
        while not _stop.is_set():
            now = time.monotonic()
            if now - last_sync >= 0.5:
                last_sync = now
                expire(now)
                add, drop = _diff(subscribed)
                for m in build_subscribe_messages(add):
                    await ws.send(m)
                for m in build_subscribe_messages(drop, REQ_UNSUBSCRIBE_DEPTH):
                    await ws.send(m)
                subscribed.update(add)
                subscribed.difference_update(drop)
                with _lock:
                    _stats["subscribed"] = len(subscribed)
                if subscribed:
                    idle_since = now
                elif now - idle_since > config.depth20_idle_close_s():
                    logger.info("dhan 20-level depth: nothing watched, closing the connection")
                    return
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if not isinstance(msg, (bytes, bytearray)):
                continue
            with _lock:
                _stats["last_packet_at"] = time.time()
            for pkt in parse_message(msg):
                kind = pkt["kind"]
                with _lock:
                    _stats["packets"] += 1
                if kind in ("bid", "ask"):
                    with _lock:
                        _stats[f"{kind}_packets"] += 1
                    apply_packet(pkt)
                elif kind == "disconnect":
                    with _lock:
                        _stats["last_disconnect_code"] = pkt.get("reason")
                    if pkt.get("reason") in _AUTH_REASONS:
                        raise DhanAuthError(f"depth feed sent disconnect code {pkt.get('reason')}")
                    if pkt.get("reason") == 805:
                        raise DhanRateLimitError("depth feed sent disconnect code 805 (too many connections)")
                    raise RuntimeError(f"depth feed sent disconnect code {pkt.get('reason')}")
                elif kind == "unknown":
                    with _lock:
                        uc = _stats["unknown_codes"]
                        uc[str(pkt.get("code"))] = uc.get(str(pkt.get("code")), 0) + 1
                else:
                    with _lock:
                        _stats["bad_packets"] += 1


def _run() -> None:
    backoff = 2.0
    while not _stop.is_set():
        try:
            from market_hours import is_feed_window_ist
            if not is_feed_window_ist():
                with _lock:
                    _stats["connected"] = False
                _stop.wait(30.0)
                continue
        except Exception:  # noqa: BLE001
            pass
        with _lock:
            has_wanted = bool(_wanted)
        if not (config.enabled() and config.depth20_enabled() and client.available()) or not has_wanted:
            with _lock:
                _stats["connected"] = False
            _stop.wait(1.0)          # lazy: a connection is opened only while somebody is asking for a book
            continue
        try:
            asyncio.run(_session())
            backoff = 2.0
        except (DhanAuthError, DhanNotConfigured) as e:
            client.note_failure(e)
            with _lock:
                _stats.update(connected=False, last_error=type(e).__name__)
            _stop.wait(60.0)
        except DhanRateLimitError as e:
            with _lock:
                _stats.update(connected=False, last_error=f"{type(e).__name__}")
            _stop.wait(120.0)
        except Exception as e:  # noqa: BLE001 - never include the URL (it holds the token)
            with _lock:
                _stats.update(connected=False, last_error=f"{type(e).__name__}: {str(e)[:80]}")
            logger.warning("dhan 20-level depth websocket dropped (%s), reconnecting in %.0fs", type(e).__name__, backoff)
            _stop.wait(backoff)
            backoff = min(60.0, backoff * 2)
        with _lock:
            _stats["connected"] = False


def start() -> None:
    global _thread
    if not (config.enabled() and config.depth20_enabled()):
        return
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_run, name="dhan-depth20", daemon=True)
    _thread.start()
    logger.info("dhan 20-level depth feed started (opt-in, DHAN_DEPTH20_ENABLED=1; connects only while symbols are asked for)")


def stop() -> None:
    _stop.set()


def status() -> dict:
    with _lock:
        out = dict(_stats)
        out["unknown_codes"] = dict(_stats["unknown_codes"])
        out["watching"] = sorted(_wanted.keys())
        out["books"] = len(_books)
    out["enabled"] = config.depth20_enabled()
    out["max_instruments"] = min(config.depth20_max_instruments(), MAX_INSTRUMENTS)
    return out


def _reset_for_tests() -> None:
    with _lock:
        _wanted.clear(); _books.clear(); _rev.clear()
        for k, v in list(_stats.items()):
            if isinstance(v, dict):
                _stats[k] = {}
            elif isinstance(v, bool):
                _stats[k] = False
            elif v is None or isinstance(v, str) or k in ("last_packet_at", "last_disconnect_code"):
                _stats[k] = None
            else:
                _stats[k] = 0
