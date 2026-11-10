"""fno/execution.py - the ONLY place that can talk to Dhan's order API. Guarded by four locks; ships with all of them closed.

REAL orders need ALL of:  1) FNO_ENGINE_ENABLED   2) FNO_MODE == REAL   3) FNO_REAL_ENABLED   4) today's ARM (POST /fno/arm, valid for the current IST date only),
5) unless FNO_REAL_REQUIRES_READINESS is off: every hard item of the readiness list is green (fno/readiness.py, round 262).
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

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple


# Round 277: a response cut short (IncompleteRead, BadStatusLine ...) is an http.client.HTTPException, which is NOT an OSError: it used to escape every call below.
_NET_ERRORS = (urllib.error.URLError, OSError, ValueError, http.client.HTTPException)


@dataclass
class OrderResult:
    ok: bool
    order_id: str = ""
    status: str = ""
    message: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


def maybe_sent(res: Any) -> bool:
    """Round 282: True when a FAILED POST /orders may still have reached Dhan, so the order could be live there. That is a network failure (timeout, cut response), an HTTP 5xx / 408 (a gateway
    can time out after Dhan's order system already took the order), or a 2xx answer without an order id. A plain 4xx (DH-905, DH-906 ...) is a clear refusal and is NOT in this class.
    Round 277 / 281 only looked at status == "NETWORK"; a 502 / 504 was reported as a refusal and the position was then sold a second time."""
    if res is None or getattr(res, "ok", False):
        return False
    s = str(getattr(res, "status", "") or "").strip().upper()
    if s in ("NETWORK", "NO_ORDER_ID"):
        return True
    if s.startswith("HTTP "):
        try:
            code = int(s[5:].split()[0])
        except (ValueError, IndexError):
            return False
        return code >= 500 or code == 408
    return False


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
    if getattr(cfg, "FNO_REAL_REQUIRES_READINESS", False):        # round 262: the readiness list (fno/readiness.py) must be all green; never computed = closed
        blockers = getattr(cfg, "FNO_READINESS_BLOCKERS", None)
        if blockers is None:
            closed.append("readiness list not computed yet (it runs every 30 s while the service is up)")
        elif blockers:
            closed.append(f"readiness list not green: {len(blockers)} hard item(s) open, first: {blockers[0]}")
    return (not closed), closed


def exit_allowed(cfg: Any, broker: Any) -> Tuple[bool, List[str]]:
    """Round 294: may a SELL / cancel for a position we ALREADY hold be sent? That needs only a broker with credentials. The four locks, today's ARM and the readiness list decide whether to OPEN
    risk (`real_allowed`); they must never stop us from closing it. Before this, a readiness blocker (a reconcile difference is one), a disarm or a restart left a REAL position
    open through its stop and the 15:15 square-off."""
    closed: List[str] = []
    if broker is None:
        closed.append("no broker (Dhan credentials not loaded: ARM borrows them)")
    elif not getattr(cfg, "DHAN_CLIENT_ID", "") or not getattr(cfg, "DHAN_ACCESS_TOKEN", ""):
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
    state: str                    # TRADED | PENDING | REJECTED | CANCELLED | UNKNOWN (normalised from Dhan's orderStatus) | NOT_FOUND (round 292: Dhan answered 404 to a lookup by correlationId)
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

    def order_by_correlation(self, correlation_id: str) -> Optional[OrderStatus]:
        """Round 292: what became of an order whose POST failed in a way that may still have reached Dhan. None = cannot tell (never read it as "not there"); state \"NOT_FOUND\" = Dhan answered 404."""
        return None


class NetPositions(dict):
    """Round 296: Dhan's open F&O net positions. As a plain dict it is {securityId: netQty} with every row of one contract SUMMED (an INTRADAY and an NRML row of the same contract add up; one used to overwrite the other).
    `by_segment` is the exact view {(exchangeSegment, securityId): netQty}: NSE and BSE can reuse a security id, so a caller that knows the segment reads it through net_qty()."""

    def __init__(self, *a: Any, **k: Any):
        super().__init__(*a, **k)
        self.by_segment: Dict[Tuple[str, str], int] = {}


def net_qty(net: Optional[Dict[str, int]], security_id: Any, segment: str = "") -> int:
    """What Dhan holds of one contract. Uses the segment when both it and the segment view are known; otherwise the id alone (a plain dict, as the test fakes return, or a row saved before segments were stored)."""
    if not net:
        return 0
    by = getattr(net, "by_segment", None)
    if by is not None and segment:
        return int(by.get((str(segment), str(security_id)), 0))
    return int(net.get(str(security_id), 0))


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
            except _NET_ERRORS:                             # round 294: a 5xx whose body is cut short (IncompleteRead) raised out of this handler, past maybe_sent / exit_doubt; the HTTP code alone decides
                body = {}
            if not isinstance(body, dict):
                body = {}
            return OrderResult(False, status=f"HTTP {e.code}", message=describe_error(body)[:200], raw=body)
        except _NET_ERRORS as e:
            return OrderResult(False, status="NETWORK", message=type(e).__name__)
        if not isinstance(body, dict):                      # round 294: a list / string answer is "no order id", which maybe_sent treats as possibly placed
            body = {}
        oid = str(body.get("orderId") or "")
        if not oid and not any(body.get(k) for k in ("errorCode", "errorType", "errorMessage", "remarks", "omsErrorDescription")):
            return OrderResult(False, status="NO_ORDER_ID", message="Dhan answered without an order id", raw=body)       # round 282: no id and no error text: the order may exist
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
        except _NET_ERRORS:
            return None
        if isinstance(body, list):
            body = body[0] if body else {}
        return parse_order_status(body if isinstance(body, dict) else {})

    def order_by_correlation(self, correlation_id: str) -> Optional[OrderStatus]:
        """Round 292: GET /v2/orders/external/{correlation-id} (Dhan's order API page lists it: "Retrieve the status of an order by correlation id"). Read only.
        An order found comes back parsed like `order_status`. A 404 is state NOT_FOUND. Anything else (network error, 5xx, 400 / 401 / 429, an answer without an orderStatus) is None: cannot tell.
        The reply shape for a missing order is not verified against the live API, so only a plain 404 counts as "not there"."""
        cid = urllib.parse.quote(str(correlation_id or "").strip(), safe="")
        if not cid:
            return None
        try:
            body = self._call("GET", f"/orders/external/{cid}")
        except urllib.error.HTTPError as e:
            return OrderStatus("NOT_FOUND", 0.0, 0, {"http": 404}) if e.code == 404 else None
        except _NET_ERRORS:
            return None
        if isinstance(body, list):
            body = body[0] if body else {}
        if not isinstance(body, dict) or not body.get("orderStatus"):
            return None
        return parse_order_status(body)

    def cancel(self, order_id: str) -> OrderResult:
        try:
            body = self._call("DELETE", f"/orders/{order_id}")
        except urllib.error.HTTPError as e:
            return OrderResult(False, status=f"HTTP {e.code}", message="cancel refused (already traded or closed?)")
        except _NET_ERRORS as e:
            return OrderResult(False, status="NETWORK", message=type(e).__name__)
        return OrderResult(True, order_id=order_id, status=str((body or {}).get("orderStatus") or "CANCELLED"))

    def net_positions(self) -> Optional[Dict[str, int]]:
        try:
            body = self._call("GET", "/positions")
        except _NET_ERRORS:
            return None
        if not isinstance(body, list):
            return None
        out = NetPositions()
        for r in body:
            if not isinstance(r, dict):                     # round 251: one odd row (null, a string) must not hide every other position from the reconcile
                continue
            try:
                q = int(r.get("netQty") or 0)
            except (TypeError, ValueError):
                continue
            seg = str(r.get("exchangeSegment") or "")
            if q and seg.endswith("FNO"):
                sid = str(r.get("securityId"))
                out[sid] = out.get(sid, 0) + q                                      # round 296: add rows of the same contract, never overwrite
                out.by_segment[(seg, sid)] = out.by_segment.get((seg, sid), 0) + q
        for k in [k for k, v in out.items() if v == 0]:                            # +65 in one row and -65 in another is flat
            del out[k]
        out.by_segment = {k: v for k, v in out.by_segment.items() if v != 0}
        return out
