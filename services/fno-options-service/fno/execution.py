"""fno/execution.py - the ONLY place that can talk to Dhan's order API. Guarded by four locks; ships with all of them closed.

REAL orders need ALL of:  1) FNO_ENGINE_ENABLED   2) FNO_MODE == REAL   3) FNO_REAL_ENABLED   4) today's ARM (POST /fno/arm, valid for the current IST date only).
Any lock closed -> `real_allowed` returns the list of closed locks and the engine only LOGS what it would have sent.

ROUND 236: the field names below were checked against Dhan's published v2 docs (dhanhq.co/docs/v2/orders and /portfolio, 8 Nov 2026): POST /orders (orderType LIMIT / MARKET / STOP_LOSS /
STOP_LOSS_MARKET, triggerPrice for the stop types, productType INTRADAY, validity DAY / IOC, correlationId up to 30 characters), GET /orders/{id} (orderStatus TRANSIT / PENDING / REJECTED / CANCELLED /
PART_TRADED / TRADED / EXPIRED, filledQty, averageTradedPrice, remainingQuantity), DELETE /orders/{id} (HTTP 202, orderId + orderStatus), GET /positions (netQty, securityId, exchangeSegment,
positionType LONG / SHORT / CLOSED). The tests use the docs' own sample JSON. What the docs do NOT show, and a live call still has to prove: how averageTradedPrice looks on a part-filled order that
was then cancelled, whether a SELL stop-limit's price must sit below its trigger, and the static-IP behaviour (an order from a non-whitelisted IP is rejected with DH-905).
Older text, kept for the record - UNVERIFIED (before round 236): the order payload follows Dhan's v2 order API (POST /v2/orders: dhanClientId, transactionType, exchangeSegment NSE_FNO / BSE_FNO, productType INTRADAY, orderType
LIMIT, validity DAY, securityId, quantity, price). It was written without network access and has NEVER been sent to Dhan. Before any real use: run it against a 1-lot, far-from-the-money
LIMIT order the market cannot fill, read the response, and compare field names with the live Dhan docs. Also needed first: the account's static IP whitelist for API orders.
Round 231: PARTIAL fills are read (filled_qty): an entry that filled in part keeps the filled lots (and gets its stop), a partial exit books the sold lots as a closed child row and keeps the rest
open, a SELL whose state is in doubt is never repeated, and a position of UNKNOWN origin is never sold unless Dhan's net position shows it (a SELL on nothing would open a short).
Round 230: with a broker that can answer, the engine waits for the fill (GET /orders/{id}), uses the real average price, cancels an order that does not fill, rests a STOP_LOSS order
at Dhan after every entry (cancelled before an exit, or closes the position if it traded first) and compares its positions with Dhan's net positions (read-only). The status / cancel / positions
calls are as unverified as the order payload. A broker that cannot answer keeps the old behaviour: recorded at the limit price and flagged `fill unconfirmed`."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class OrderResult:
    ok: bool
    order_id: str = ""
    status: str = ""
    message: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


def real_allowed(cfg: Any, armed_day: Optional[str], today: str) -> Tuple[bool, List[str]]:
    closed: List[str] = []
    if not getattr(cfg, "FNO_ENGINE_ENABLED", False):
        closed.append("FNO_ENGINE_ENABLED is off")
    if str(getattr(cfg, "FNO_MODE", "DEMO")).upper() != "REAL":
        closed.append("FNO_MODE is not REAL")
    if not getattr(cfg, "FNO_REAL_ENABLED", False):
        closed.append("FNO_REAL_ENABLED is off")
    if armed_day != today:
        closed.append("not armed today (POST /fno/arm)")
    if not getattr(cfg, "DHAN_CLIENT_ID", "") or not getattr(cfg, "DHAN_ACCESS_TOKEN", ""):
        closed.append("no Dhan client id / access token configured")
    return (not closed), closed


def build_order(client_id: str, security_id: int, segment: str, side: str, qty: int, price: float, correlation_id: Optional[str] = None) -> Dict[str, Any]:
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    if qty <= 0 or price <= 0 or not security_id:
        raise ValueError("an order needs a positive quantity, a positive price and a security id")
    if segment not in ("NSE_FNO", "BSE_FNO"):
        raise ValueError("segment must be NSE_FNO or BSE_FNO")
    return {"dhanClientId": client_id, "correlationId": (correlation_id or uuid.uuid4().hex[:20]), "transactionType": side, "exchangeSegment": segment,
            "productType": "INTRADAY", "orderType": "LIMIT", "validity": "DAY", "securityId": str(int(security_id)), "quantity": int(qty),
            "disclosedQuantity": 0, "price": round(float(price), 2), "afterMarketOrder": False}


@dataclass
class OrderStatus:
    state: str                    # TRADED | PENDING | REJECTED | CANCELLED | UNKNOWN (normalised from Dhan's orderStatus)
    avg_price: float = 0.0
    filled_qty: int = 0
    raw: Dict[str, Any] = field(default_factory=dict)


_STATE = {"TRADED": "TRADED", "PART_TRADED": "PENDING", "TRANSIT": "PENDING", "PENDING": "PENDING", "TRIGGER_PENDING": "PENDING", "CONFIRM": "PENDING",
          "REJECTED": "REJECTED", "CANCELLED": "CANCELLED", "EXPIRED": "CANCELLED"}


def parse_order_status(body: Dict[str, Any]) -> OrderStatus:
    """GET /v2/orders/{id} -> orderStatus, averageTradedPrice, filledQty (names checked against Dhan's docs in round 236).
    Round 237 hardening: Dhan's docs do not say what a TRADED order shows when averageTradedPrice or filledQty is 0 / missing. Every caller treats "TRADED with no price" as "not filled", which for
    a resting STOP would leave the position OPEN in our books after the broker is already flat, and the next square-off would then SELL and leave a short. So a TRADED order always comes back
    with a price (its own limit `price`, flagged `raw["_avg_estimated"]`) and a quantity (its own `quantity`): a slightly wrong price is booked and noted, a missed fill never is."""
    st = _STATE.get(str(body.get("orderStatus") or "").upper(), "UNKNOWN")
    try:
        avg = float(body.get("averageTradedPrice") or 0)
    except (TypeError, ValueError):
        avg = 0.0
    try:
        q = int(body.get("filledQty") or body.get("tradedQuantity") or 0)
    except (TypeError, ValueError):
        q = 0
    raw = dict(body)
    if st == "TRADED":
        if q <= 0:
            try:
                q = int(body.get("quantity") or 0)
            except (TypeError, ValueError):
                q = 0
            raw["_filled_estimated"] = True
        if avg <= 0:
            try:
                px = float(body.get("price") or 0)
            except (TypeError, ValueError):
                px = 0.0
            if px > 0:
                avg = px
                raw["_avg_estimated"] = True
    return OrderStatus(st, avg, q, raw)


def build_sl_order(client_id: str, security_id: int, segment: str, qty: int, trigger: float, limit_gap_pct: float = 5.0, tick: float = 0.05,
                   correlation_id: Optional[str] = None) -> Dict[str, Any]:
    """A resting SELL stop-loss (orderType STOP_LOSS) for a long option: triggers at `trigger`, then sells at a limit `limit_gap_pct` % below it (so it can fill in a fast fall).
    UNVERIFIED payload (triggerPrice / STOP_LOSS names): test with a 1-lot far-away order before trusting it."""
    if qty <= 0 or trigger <= 0 or not security_id:
        raise ValueError("an SL order needs a positive quantity, trigger and security id")
    limit = max(tick, round(round(trigger * (1 - limit_gap_pct / 100.0) / tick) * tick, 2))
    o = build_order(client_id, security_id, segment, "SELL", qty, limit, correlation_id)
    o.update(orderType="STOP_LOSS", triggerPrice=round(float(trigger), 2))
    return o


# Dhan error codes worth saying in plain words (the code is always kept in the message). DH-905 is the static-IP / invalid-request family: Dhan's own support page names a non-whitelisted
# source IP as its usual cause for an order.
_DH_HINT = {"DH-901": "invalid authentication: the access token is expired or wrong (renew it)", "DH-902": "no access to the Data / Trading API for this account",
            "DH-903": "account-level problem: check that the F&O segment is active", "DH-904": "rate limit: too many requests", "DH-905": "invalid request or IP not whitelisted: check the static IP in Dhan's API settings (and renew the token after changing it)",
            "DH-906": "order error (see the message)", "DH-907": "data could not be fetched", "DH-908": "internal server error at Dhan", "DH-909": "network error between Dhan and the exchange", "DH-910": "other error"}


def describe_error(body: Any) -> str:
    """One readable line from a Dhan error body ({errorType, errorCode, errorMessage} or {remarks...}); the DH-9xx code is kept and, when known, explained."""
    if not isinstance(body, dict):
        return "rejected"
    code = str(body.get("errorCode") or "").strip()
    msg = str(body.get("errorMessage") or body.get("remarks") or body.get("omsErrorDescription") or "rejected").strip()
    hint = _DH_HINT.get(code, "")
    return " ".join(x for x in (code + ":" if code else "", msg, f"({hint})" if hint else "") if x)


class Broker:
    # True = this broker CAN report an order's state, so a missing answer (network error) means "in doubt", never "assume it went through".
    # False (the base class, and the test fakes that predate round 231) = it cannot answer at all: the engine keeps the old behaviour (limit price, flagged unconfirmed).
    can_confirm = False

    def place(self, payload: Dict[str, Any]) -> OrderResult:
        raise NotImplementedError

    # The three below default to "cannot tell": the engine then behaves as before (position recorded at the limit price, flagged unconfirmed).
    def order_status(self, order_id: str) -> Optional[OrderStatus]:
        return None

    def cancel(self, order_id: str) -> OrderResult:
        return OrderResult(False, message="cancel not supported")

    def net_positions(self) -> Optional[Dict[str, int]]:
        return None


class DhanBroker(Broker):
    can_confirm = True

    def __init__(self, base_url: str, client_id: str, access_token: str, timeout_s: float = 10.0, opener: Optional[Callable[..., Any]] = None):
        self.base, self.client_id, self.token, self.timeout = base_url.rstrip("/"), client_id, access_token, timeout_s
        self._open = opener or urllib.request.urlopen

    def place(self, payload: Dict[str, Any]) -> OrderResult:
        req = urllib.request.Request(self.base + "/orders", data=json.dumps(payload).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json", "Accept": "application/json", "access-token": self.token, "client-id": self.client_id})
        try:
            with self._open(req, timeout=self.timeout) as r:
                body = json.loads(r.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8") or "{}")
            except ValueError:
                body = {}
            return OrderResult(False, status=f"HTTP {e.code}", message=describe_error(body)[:200], raw=body)
        except (urllib.error.URLError, OSError, ValueError) as e:
            return OrderResult(False, status="NETWORK", message=type(e).__name__)
        oid = str(body.get("orderId") or "")
        return OrderResult(bool(oid), order_id=oid, status=str(body.get("orderStatus") or ""), message=str(body.get("errorMessage") or "")[:200], raw=body)


    # ── status / cancel / positions (UNVERIFIED endpoints: GET /orders/{id}, DELETE /orders/{id}, GET /positions) ──
    def _call(self, method: str, path: str) -> Any:
        req = urllib.request.Request(self.base + path, method=method, headers={"Accept": "application/json", "access-token": self.token, "client-id": self.client_id})
        with self._open(req, timeout=self.timeout) as r:
            raw = r.read().decode("utf-8") or "{}"
        return json.loads(raw)

    def order_status(self, order_id: str) -> Optional[OrderStatus]:
        try:
            body = self._call("GET", f"/orders/{order_id}")
        except (urllib.error.URLError, OSError, ValueError):
            return None
        if isinstance(body, list):
            body = body[0] if body else {}
        return parse_order_status(body if isinstance(body, dict) else {})

    def cancel(self, order_id: str) -> OrderResult:
        try:
            body = self._call("DELETE", f"/orders/{order_id}")
        except urllib.error.HTTPError as e:
            return OrderResult(False, status=f"HTTP {e.code}", message="cancel refused (already traded or closed?)")
        except (urllib.error.URLError, OSError, ValueError) as e:
            return OrderResult(False, status="NETWORK", message=type(e).__name__)
        return OrderResult(True, order_id=order_id, status=str((body or {}).get("orderStatus") or "CANCELLED"))

    def net_positions(self) -> Optional[Dict[str, int]]:
        try:
            body = self._call("GET", "/positions")
        except (urllib.error.URLError, OSError, ValueError):
            return None
        if not isinstance(body, list):
            return None
        out: Dict[str, int] = {}
        for r in body:
            if not isinstance(r, dict):                     # round 251: one odd row (null, a string) must not hide every other position from the reconcile
                continue
            try:
                q = int(r.get("netQty") or 0)
            except (TypeError, ValueError):
                continue
            if q and str(r.get("exchangeSegment") or "").endswith("FNO"):
                out[str(r.get("securityId"))] = q
        return out
