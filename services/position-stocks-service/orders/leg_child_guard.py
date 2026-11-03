"""orders/leg_child_guard.py - E285 round 167: cancel the order Dhan itself created from a triggered Super Order leg, BEFORE an exit order is sent.

WHY (JYOTICNC, 3 Nov 2026): a SHORT's stop leg triggered at 12:09:11 and Dhan turned it into its own plain order (BUY LIMIT 929.45, tag VTT_SQROFF, a separate order id).
Price stayed above that limit, so the order rested without filling. The stop watchdog then saw the price sitting past the stop and covered at market (12:09:39, 937.25).
Cancelling the three Super Order legs does nothing to a leg that already triggered, and the leg guard (_filled_bracket_exit_leg) only fires once the leg is TRADED, which it
was not yet. At 12:11:41 price fell to 929.45, the leftover order filled, and the account was LONG 1 with the app believing it was flat.

WHAT: for a position that has a Super Order (and no plain stop of its own), read the order book and look for orders that are
  * for this security, on the EXIT side of the position (BUY for a short, SELL for a long),
  * created at or after the position opened,
  * tagged with Dhan's own VTT prefix (the app never uses that tag; its own exit orders are left alone, the existing retry logic adopts those).
Result of release_children(db, pos) -> (state, why):
  'none'       nothing to do (no such order, all of them already dead, guard off, no Super Order, order book unreadable: fail open like the bracket-leg guard),
  'cancelled'  every live one was cancelled and read back dead: the exit may go,
  'filled'     such an order already filled the whole position: it is flat, NO exit may be sent (reconcile books the fill from the legs),
  'live'       one could not be cancelled and is still live: the exit STILL goes (an uncovered short that is past its stop is the worse risk) and a critical alert names the order;
               if that order fills later, orders/residual_guard.py raises the mismatch alert,
  'error'      one filled in part: the exit size is not known, NO exit is sent now; the exit-retry backoff tries again.
Never raises. Switch: LEG_CHILD_GUARD (default true).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple

logger = logging.getLogger("position-stocks-leg-child-guard")

CHILD_TAG_PREFIX = "VTT"
LIVE = {"TRANSIT", "PENDING", "OPEN", "PART_TRADED", "PARTIALLY_TRADED", "TRIGGER_PENDING", "AMO_SUBMITTED"}
FILLED = {"TRADED", "FILLED", "EXECUTED", "COMPLETE", "COMPLETED"}
_IST = timezone(timedelta(hours=5, minutes=30))


def _cfg(name: str, default: Any) -> Any:
    try:
        import config
        return getattr(config, name, default)
    except Exception:  # noqa: BLE001
        return default


def _dhan() -> Any:
    from execution import dhan_client
    return dhan_client


def _status(row: Optional[dict]) -> str:
    r = row or {}
    return str(r.get("orderStatus") or r.get("order_status") or r.get("status") or "").upper()


def _oid(row: dict) -> str:
    return str(row.get("orderId") or row.get("order_id") or "")


def _filled(row: Optional[dict]) -> int:
    r = row or {}
    try:
        return int(float(r.get("filledQty") or r.get("filled_qty") or r.get("tradedQuantity") or 0))
    except (TypeError, ValueError):
        return 0


def _partly(row: Optional[dict], qty: int) -> bool:
    return 1 <= _filled(row) < int(qty) and _status(row) not in FILLED


def _created_before(row: dict, since: Optional[datetime]) -> bool:
    """True only when the row has a parseable createTime earlier than `since` (Dhan stamps naive IST; `since` is the naive-UTC opened_at). Unparseable = False (row kept)."""
    if since is None:
        return False
    try:
        raw = row.get("createTime") or row.get("create_time")
        t = datetime.fromisoformat(str(raw).replace("Z", "+00:00").replace(" ", "T")[:19])
        if t.tzinfo is None:
            t = t.replace(tzinfo=_IST)
        s = since if since.tzinfo is not None else since.replace(tzinfo=timezone.utc)
        return t < s
    except Exception:  # noqa: BLE001
        return False


def find_children(rows: list, pos: Any, exit_side: str) -> list:
    """Pure. The Dhan-made leg orders of this position (see the module docstring)."""
    out = []
    sid = str(getattr(pos, "dhan_security_id", "") or "")
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        if str(r.get("securityId") or r.get("security_id") or "") != sid:
            continue
        if str(r.get("transactionType") or r.get("transaction_type") or "").upper() != exit_side:
            continue
        if not str(r.get("correlationId") or r.get("tag") or "").upper().startswith(CHILD_TAG_PREFIX):
            continue
        if _created_before(r, getattr(pos, "opened_at", None)):
            continue
        out.append(r)
    return out


def filled_child_exit(rows: list, pos: Any, exit_side: str) -> Optional[dict]:
    """Pure. E285 round 169 (RATNAVEER, 3 Nov): the ONE Dhan-made leg order (tag VTT...) of this position that already filled its whole quantity, as
    {"order_id", "price"}, or None. None also when there are two or more such orders (cannot tell which is the exit) or when the fill has no traded price.
    Dhan's Super Order list can lag or carry no fill price on a triggered leg; this order in the plain order book is the real exit."""
    qty = int(getattr(pos, "quantity", 0) or 0)
    hits = []
    for r in find_children(rows, pos, exit_side):
        if _status(r) not in FILLED or qty <= 0:
            continue
        if _filled(r) not in (0, qty) and _filled(r) < qty:
            continue
        try:
            px = float(r.get("averageTradedPrice") or r.get("average_traded_price") or 0)
        except (TypeError, ValueError):
            px = 0.0
        if px > 0:
            hits.append({"order_id": _oid(r), "price": px})
    return hits[0] if len(hits) == 1 else None


def _alert(text: str) -> None:
    try:
        import notifier
        notifier.notify_critical(text)
    except Exception:  # noqa: BLE001
        logger.warning("leg child guard: alert not sent", exc_info=True)


def release_children(db: Any, pos: Any, sleep=time.sleep) -> Tuple[str, str]:
    try:
        return _release(db, pos, sleep)
    except Exception:  # noqa: BLE001 - an exit must never be stopped by this check failing
        logger.warning("leg child guard: failed for %s, proceeding", getattr(pos, "symbol", "?"), exc_info=True)
        return "none", ""


def _release(db: Any, pos: Any, sleep) -> Tuple[str, str]:
    if not _cfg("LEG_CHILD_GUARD", True):
        return "none", ""
    if not getattr(pos, "dhan_super_order_id", None) or getattr(pos, "overnight_converted_to_cnc", False) or getattr(pos, "plain_stop_order_id", None):
        return "none", ""
    from orders import side as _side
    exit_side = _side.exit_transaction_type(pos)
    dhan = _dhan()
    try:
        rows = dhan.get_order_list(db)
    except Exception as e:  # noqa: BLE001
        logger.warning("leg child guard: %s order book unreadable (%s), proceeding", getattr(pos, "symbol", "?"), e)
        return "none", ""
    kids = find_children(rows, pos, exit_side)
    if not kids:
        return "none", ""
    qty = int(getattr(pos, "quantity", 0) or 0)
    sym = getattr(pos, "symbol", "?")
    for r in kids:
        if (_status(r) in FILLED and _filled(r) in (0, qty)) or (qty > 0 and _filled(r) >= qty):
            return "filled", f"Dhan order {_oid(r)} ({r.get('correlationId')}) already filled {sym}"
    for r in kids:
        if _partly(r, qty):
            return "error", f"Dhan order {_oid(r)} ({r.get('correlationId')}) filled in part ({_filled(r)} of {qty}): no exit was sent, check Dhan"
    live = [r for r in kids if _status(r) in LIVE]
    if not live:
        return "none", ""
    ids = [_oid(r) for r in live]
    for oid in ids:
        try:
            dhan.cancel_order(db, is_armed=True, dhan_order_id=oid)
        except Exception as e:  # noqa: BLE001 - may have filled meanwhile: the read-back decides
            logger.info("leg child guard: cancel of %s (%s) not done: %s", oid, sym, e)
    sleep(float(_cfg("MANUAL_EXIT_CANCEL_WAIT_S", 0.5) or 0.0))
    try:
        after = {_oid(r): r for r in dhan.get_order_list(db) if isinstance(r, dict)}
    except Exception as e:  # noqa: BLE001
        after = {}
        logger.warning("leg child guard: %s read-back failed (%s)", sym, e)
    still = []
    for oid in ids:
        row = after.get(oid)
        st = _status(row)
        if row is not None and (st in FILLED or _filled(row) >= qty > 0):
            return "filled", f"Dhan order {oid} filled while it was being cancelled"
        if row is not None and _partly(row, qty):
            return "error", f"Dhan order {oid} filled in part while it was being cancelled ({_filled(row)} of {qty}): no exit was sent, check Dhan"
        if row is None or st in LIVE:
            still.append(oid)
    if still:
        why = f"leg order(s) {', '.join(still)} on {sym} could not be cancelled and are still live; the exit is sent anyway"
        _alert(f"LEG ORDER STILL LIVE: {why}. If one fills after the exit, the account will hold the opposite position. Check Dhan.")
        return "live", why
    return "cancelled", f"cancelled Dhan leg order(s) {', '.join(ids)} on {sym} before the exit"
