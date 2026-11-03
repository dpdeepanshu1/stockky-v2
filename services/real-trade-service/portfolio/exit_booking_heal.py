"""portfolio/exit_booking_heal.py - E285 round 168: two pure helpers for holdings_sync_reconcile (stdlib only, no I/O).

WHY (CHOICEIN, 3 Nov 2026): the 15:00 square-off SOLD 1 CHOICEIN at 746.05 and the fill was booked (a PARTIAL_EXIT event, -10.40, broker-confirmed),
yet the position row stayed OPEN with qty_open 1 and realized_pnl 0 (the EXIT_ORDER_DEAD restore ran 25 s later and wrote OPEN). Dhan was flat, but the heal
pass never closed the row, because it read a closed same-day CNC row (netQty 0, buyQty 1) as "still held": max(netQty, buyQty) = 1.

broker_row_qty: the shares a Dhan positions row says we HOLD. When the row carries a readable netQty it is the answer (>0 held, 0 or less = nothing held), exactly
as portfolio/broker_view.py and the import path (group 180) already read it. Only a row with no readable netQty falls back to the old max(buy-side) reading.

booked_exit_totals: (shares, pnl) of the broker-confirmed exits already written as position events ("<reason>: <qty> @ <price> (pnl +x.xx, broker-confirmed)").
Used when the position row lost its own decrement: the exit's cash was credited in the same commit as its event, so closing the row must not refund that cash again.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Tuple

_EXIT_RE = re.compile(r":\s*(\d+)\s*@\s*([0-9]+(?:\.[0-9]+)?)\s*\(pnl\s*([+-]?[0-9]+(?:\.[0-9]+)?)[^)]*broker-confirmed")


def _int(v: Any):
    try:
        if v is None or v == "":
            return None
        return int(float(v))
    except (TypeError, ValueError):
        return None


def broker_row_qty(row: Mapping, net_authoritative: bool = True) -> int:
    """Pure. Shares held according to one Dhan positions row (never negative)."""
    if net_authoritative:
        for k in ("netQty", "net_qty"):
            n = _int(row.get(k))
            if n is not None:
                return max(n, 0)
    best = 0
    for keys in (("netQty", "net_qty"), ("positiveQty", "positive_qty", "buyQty", "buy_qty")):
        for k in keys:
            n = _int(row.get(k))
            if n is not None:
                best = max(best, n)
                break
    return best


def booked_exit_totals(events: Iterable[Tuple[str, str]]) -> Tuple[int, float]:
    """Pure. Sum (shares, pnl) over (event_type, detail) pairs of PARTIAL_EXIT / CLOSED events written by record_real_exit_fill."""
    qty, pnl = 0, 0.0
    for et, detail in events or []:
        if str(et) not in ("PARTIAL_EXIT", "CLOSED"):
            continue
        m = _EXIT_RE.search(str(detail or ""))
        if m:
            qty += int(m.group(1))
            pnl += float(m.group(3))
    return qty, round(pnl, 2)
