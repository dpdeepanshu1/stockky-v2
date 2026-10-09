"""Dhan live order updates over WebSocket (group 280, plan Phase C1) - READ-ONLY listener.

Dhan pushes every order status change to wss://api-order-update.dhan.co after a login message:
    {"LoginReq": {"MsgCode": 42, "ClientId": "<id>", "Token": "<access token>"}, "UserType": "SELF"}
Each message is JSON {"Type": "order_alert", "Data": {OrderNo, Status (TRANSIT/PENDING/REJECTED/CANCELLED/TRADED/
EXPIRED), TradedQty, AvgTradedPrice, TradedPrice, RemainingQuantity, Symbol, TxnType (B/S), ProductName, LegNo,
CorrelationId, ReasonDescription ...}}.

What this module does: keeps the latest state (and the last few transitions) of every order in memory, so that
reconcile can read a REAL event instead of inferring a state from a poll. It places, modifies and cancels nothing, and
nothing in the trading path depends on it: with DHAN_ORDER_WS_ENABLED=0 (the default) it is never started, and when
it is on and the socket is down, `latest()` simply returns None and every caller keeps using the REST poll.

Reconnects with back-off, re-reading the stored credentials each time (the token is replaced every 24 hours). The
token is never logged. Message shapes are taken from Dhan's documentation and have NOT been checked against a live
session: the first live run must be read from GET /orders/ws-status and the debug log before anyone relies on it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import OrderedDict, deque
from typing import Callable, Optional

logger = logging.getLogger("position-stocks-order-ws")

WS_URL = "wss://api-order-update.dhan.co"
_MAX_ORDERS = 5000
_MAX_TRANSITIONS = 20
_STATUSES = ("TRANSIT", "PENDING", "REJECTED", "CANCELLED", "TRADED", "EXPIRED", "PART_TRADED", "TRIGGERED")


def _f(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _i(v) -> Optional[int]:
    f = _f(v)
    return int(f) if f is not None else None


def parse_message(raw) -> Optional[dict]:
    """A normalised order event from one raw WebSocket message, or None when it is not an order update."""
    try:
        msg = json.loads(raw) if isinstance(raw, (str, bytes, bytearray)) else raw
        if not isinstance(msg, dict):
            return None
        data = msg.get("Data")
        if not isinstance(data, dict):
            return None
        order_no = str(data.get("OrderNo") or data.get("orderNo") or "").strip()
        if not order_no:
            return None
        status = str(data.get("Status") or "").strip().upper().replace(" ", "_")
        return {
            "order_id": order_no,
            "exch_order_id": str(data.get("ExchOrderNo") or "").strip() or None,
            "status": status or None,
            "symbol": str(data.get("Symbol") or "").strip().upper() or None,
            "txn": str(data.get("TxnType") or "").strip().upper()[:1] or None,       # B / S
            "product": str(data.get("ProductName") or data.get("Product") or "").strip().upper() or None,
            "leg_no": _i(data.get("LegNo")),
            "quantity": _i(data.get("Quantity")),
            "traded_qty": _i(data.get("TradedQty")),
            "remaining_qty": _i(data.get("RemainingQuantity")),
            "avg_traded_price": _f(data.get("AvgTradedPrice")),
            "traded_price": _f(data.get("TradedPrice")),
            "price": _f(data.get("Price")),
            "trigger_price": _f(data.get("TriggerPrice")),
            "correlation_id": str(data.get("CorrelationId") or "").strip() or None,
            "reason": str(data.get("ReasonDescription") or "").strip() or None,
            "type": str(msg.get("Type") or "").strip() or None,
            "received_at": time.time(),
        }
    except Exception as e:  # noqa: BLE001
        logger.debug("order ws: unparseable message ignored: %s", e)
        return None


class OrderEventStore:
    """Thread-safe latest-state-per-order store with a short transition history. Bounded; oldest orders drop first."""

    def __init__(self, max_orders: int = _MAX_ORDERS):
        self._lock = threading.Lock()
        self._orders: "OrderedDict[str, dict]" = OrderedDict()
        self._max = max_orders

    def add(self, ev: dict) -> None:
        oid = ev["order_id"]
        with self._lock:
            cur = self._orders.pop(oid, None)
            hist = cur["history"] if cur else deque(maxlen=_MAX_TRANSITIONS)
            hist.append({"status": ev.get("status"), "traded_qty": ev.get("traded_qty"),
                         "avg_traded_price": ev.get("avg_traded_price"), "at": ev["received_at"]})
            self._orders[oid] = {"event": ev, "history": hist}
            while len(self._orders) > self._max:
                self._orders.popitem(last=False)

    def latest(self, order_id) -> Optional[dict]:
        with self._lock:
            cur = self._orders.get(str(order_id))
            return dict(cur["event"]) if cur else None

    def history(self, order_id) -> list:
        with self._lock:
            cur = self._orders.get(str(order_id))
            return list(cur["history"]) if cur else []

    def __len__(self) -> int:
        with self._lock:
            return len(self._orders)

    def recent(self, limit: int = 50) -> list:
        with self._lock:
            return [dict(v["event"]) for v in list(self._orders.values())[-limit:]][::-1]


store = OrderEventStore()


def login_message(client_id: str, token: str) -> str:
    return json.dumps({"LoginReq": {"MsgCode": 42, "ClientId": str(client_id), "Token": token}, "UserType": "SELF"})


class OrderUpdateListener:
    """Connect, log in, feed `store`; reconnect with back-off until stopped. `connect` is injectable for tests."""

    def __init__(self, get_creds: Callable[[], Optional[tuple]], connect: Optional[Callable] = None,
                 event_store: Optional[OrderEventStore] = None, url: str = WS_URL,
                 backoff_min: float = 2.0, backoff_max: float = 300.0):
        self._get_creds = get_creds
        self._connect = connect
        self._store = event_store if event_store is not None else store
        self._url = url
        self._bmin, self._bmax = backoff_min, backoff_max
        self._stop = False
        self.state = {"enabled": True, "connected": False, "connects": 0, "messages": 0, "order_events": 0,
                      "last_message_at": None, "last_error": None, "last_connect_at": None, "no_credentials": 0}

    def stop(self) -> None:
        self._stop = True

    def status(self) -> dict:
        return {**self.state, "orders_tracked": len(self._store), "url": self._url}

    async def _open(self):
        if self._connect is not None:
            return await self._connect(self._url)
        import websockets
        return await websockets.connect(self._url, ping_interval=20, ping_timeout=20, max_size=2 ** 20)

    async def run(self) -> None:
        delay = self._bmin
        while not self._stop:
            try:
                creds = self._get_creds()
                if not creds:
                    self.state["no_credentials"] += 1
                    self.state["last_error"] = "no Dhan credentials stored"
                    await asyncio.sleep(min(delay, 60.0))
                    continue
                client_id, token = creds
                ws = await self._open()
                try:
                    await ws.send(login_message(client_id, token))
                    self.state.update(connected=True, last_connect_at=time.time(), last_error=None)
                    self.state["connects"] += 1
                    delay = self._bmin
                    async for raw in ws:
                        self.state["messages"] += 1
                        self.state["last_message_at"] = time.time()
                        ev = parse_message(raw)
                        if ev:
                            self._store.add(ev)
                            self.state["order_events"] += 1
                        if self._stop:
                            break
                finally:
                    self.state["connected"] = False
                    try:
                        await ws.close()
                    except Exception:  # noqa: BLE001
                        pass
            except asyncio.CancelledError:
                self.state["connected"] = False
                raise
            except Exception as e:  # noqa: BLE001
                # never put the token in a log line; the exception text of websockets does not carry it
                self.state["connected"] = False
                self.state["last_error"] = f"{type(e).__name__}: {str(e)[:160]}"
                logger.warning("order ws: %s - reconnecting in %.0fs", self.state["last_error"], delay)
            if self._stop:
                break
            await asyncio.sleep(delay)
            delay = min(delay * 2, self._bmax)
        self.state["connected"] = False
